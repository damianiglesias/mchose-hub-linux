#!/usr/bin/env bash
# =============================================================================
#  MCHOSE Linux Hub — instalador
#
#  Uso:    bash install.sh [--no-deps] [--group NOMBRE]
#          bash install.sh --uninstall
#
#  Qué hace (con un solo comando; pedirá tu contraseña de sudo si hace falta):
#    1. Detecta la distribución e instala dependencias (GTK 4, libadwaita, hidapi…).
#    2. Verifica que Python puede importar GTK 4/libadwaita y hidapi
#       (si falta hidapi, crea un entorno virtual y lo instala con pip).
#    3. Copia la aplicación a /opt/mchose-linux-hub y crea el lanzador /usr/local/bin/mchose-hub.
#    4. Genera e instala las reglas udev (acceso al hardware SIN ser root) y las recarga.
#    5. Crea el acceso directo .desktop en el menú de aplicaciones.
#
#  Opciones:
#    --no-deps       no instala paquetes del sistema (sólo verifica que estén)
#    --group NOMBRE  reglas udev por grupo en vez de «uaccess» (para sistemas sin systemd-logind);
#                    crea el grupo y añade a tu usuario
#    --uninstall     elimina todo lo instalado por este script
# =============================================================================
set -Eeuo pipefail

APP_NAME="mchose-hub"
APP_ID="io.github.MchoseLinuxHub"
APP_DIR="/opt/mchose-linux-hub"
BIN_PATH="/usr/local/bin/${APP_NAME}"
DESKTOP_DIR="/usr/local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
RULES_FILE="/etc/udev/rules.d/70-mchose-hub.rules"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIG_ARGS=("$@")          # se conservan para relanzar con sudo
INSTALL_DEPS=1
UDEV_GROUP=""
DO_UNINSTALL=0
PM="unknown"
PYEXEC=""

# --- mensajes ---------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_BLD=$'\e[1m'; C_OFF=$'\e[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLD=""; C_OFF=""
fi
log()  { printf '%s==>%s %s\n' "$C_BLD" "$C_OFF" "$*"; }
ok()   { printf '%s ✔%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s ⚠%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%s ✖ %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 1; }
trap 'printf "%s ✖ Error inesperado en la línea %s de install.sh%s\n" "$C_RED" "$LINENO" "$C_OFF" >&2' ERR

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --- argumentos ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-deps)   INSTALL_DEPS=0 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    --group)
      [[ $# -ge 2 ]] || die "--group necesita un nombre de grupo"
      UDEV_GROUP="$2"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "Opción desconocida: $1 (usa --help)" ;;
  esac
  shift
done

# --- privilegios -------------------------------------------------------------
ensure_root() {
  if [[ $EUID -eq 0 ]]; then return 0; fi
  command -v sudo >/dev/null 2>&1 \
    || die "Hacen falta privilegios de administrador y 'sudo' no está instalado. Ejecuta este script como root."
  log "Se necesitan privilegios de administrador; relanzo con sudo…"
  exec sudo -E bash "${BASH_SOURCE[0]}" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
}

# --- udev ---------------------------------------------------------------------
reload_udev() {
  if ! command -v udevadm >/dev/null 2>&1; then
    warn "udevadm no está disponible: reinicia o reconecta tus dispositivos para aplicar las reglas"
    return 0
  fi
  if udevadm control --reload-rules \
     && udevadm trigger --action=add --subsystem-match=hidraw --subsystem-match=usb; then
    ok "Reglas udev recargadas y aplicadas a lo que ya estaba conectado"
  else
    warn "No se pudieron recargar las reglas (¿udev no está en ejecución?). Reconecta el dispositivo o reinicia."
  fi
}

# --- desinstalación ---------------------------------------------------------------
uninstall() {
  log "Desinstalando MCHOSE Linux Hub…"
  rm -rf "$APP_DIR"
  rm -f "$BIN_PATH" "$DESKTOP_FILE" "$RULES_FILE"
  reload_udev
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  fi
  ok "Desinstalado. (Los paquetes del sistema y /etc/mchose-hub no se han tocado.)"
}

# --- 1. dependencias del sistema -------------------------------------------------
detect_pm() {
  if   command -v rpm-ostree >/dev/null 2>&1 && [[ -e /run/ostree-booted ]]; then PM="ostree"
  elif command -v apt-get    >/dev/null 2>&1; then PM="apt"
  elif command -v dnf        >/dev/null 2>&1; then PM="dnf"
  elif command -v pacman     >/dev/null 2>&1; then PM="pacman"
  elif command -v zypper     >/dev/null 2>&1; then PM="zypper"
  else PM="unknown"
  fi
}

pm_install() {   # pm_install paquete…   (devuelve ≠0 si falla)
  case "$PM" in
    apt)    apt-get install -y --no-install-recommends "$@" ;;
    dnf)    dnf install -y "$@" ;;
    pacman) pacman -S --needed --noconfirm "$@" ;;
    zypper) zypper --non-interactive install --no-recommends "$@" ;;
    *)      return 1 ;;
  esac
}

install_deps() {
  local -a required=() optional=()
  case "$PM" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      required=(python3 python3-venv python3-pip python3-gi gir1.2-gtk-4.0 gir1.2-adw-1)
      optional=(libhidapi-hidraw0 python3-hid python3-pyudev)
      apt-get update -y >/dev/null || warn "apt-get update falló; sigo con la caché local" ;;
    dnf)
      required=(python3 python3-pip python3-gobject gtk4 libadwaita)
      optional=(hidapi python3-hidapi python3-pyudev) ;;
    pacman)
      required=(python python-pip python-gobject gtk4 libadwaita)
      optional=(hidapi python-hidapi python-pyudev) ;;
    zypper)
      required=(python3 python3-pip python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1)
      optional=(libhidapi-hidraw0 python3-pyudev python3-hidapi) ;;
    ostree|unknown)
      warn "Gestor de paquetes no soportado o sistema inmutable ($PM): no instalo paquetes."
      warn "Necesitas: Python ≥ 3.9, PyGObject, GTK 4, libadwaita y hidapi. Sigo y compruebo lo que haya."
      return 0 ;;
  esac

  log "Instalando dependencias obligatorias: ${required[*]}"
  pm_install "${required[@]}" \
    || die "No se pudieron instalar las dependencias obligatorias. Si tu distribución es antigua (sin GTK 4 + libadwaita 1) esta aplicación no es compatible."

  local pkg
  for pkg in "${optional[@]}"; do
    if pm_install "$pkg" >/dev/null 2>&1; then ok "paquete opcional instalado: $pkg"
    else warn "paquete opcional no disponible: $pkg (no es grave)"; fi
  done
}

# --- 2. verificación de Python / hidapi ------------------------------------------
check_gtk() { "$1" -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' >/dev/null 2>&1; }
check_hid() { "$1" -c 'import hid; hid.enumerate' >/dev/null 2>&1; }

prepare_python() {
  command -v python3 >/dev/null 2>&1 || die "No se encuentra python3."
  local py; py="$(command -v python3)"
  "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Se necesita Python ≥ 3.9 (tienes $("$py" -V 2>&1))."
  check_gtk "$py" \
    || die "Python no puede importar GTK 4 / libadwaita. En Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
  ok "GTK 4 y libadwaita disponibles"

  if check_hid "$py"; then
    PYEXEC="$py"; ok "hidapi disponible en el Python del sistema"; return 0
  fi
  warn "hidapi no está en el Python del sistema: creo un entorno virtual en $APP_DIR/venv"
  install -d -m 755 "$APP_DIR"
  rm -rf "$APP_DIR/venv"
  "$py" -m venv --system-site-packages "$APP_DIR/venv" \
    || die "No se pudo crear el entorno virtual (¿falta el paquete python3-venv?)."
  "$APP_DIR/venv/bin/pip" install --quiet hidapi \
    || die "pip no pudo instalar 'hidapi'. Instala el paquete de tu distro (python3-hid / python-hidapi) o las herramientas de compilación (gcc, python3-dev, libusb-1.0, libudev) y repite."
  check_hid "$APP_DIR/venv/bin/python" || die "El módulo 'hid' sigue sin poder importarse en el entorno virtual."
  PYEXEC="$APP_DIR/venv/bin/python"
  ok "hidapi instalado en el entorno virtual"
}

# --- 3. aplicación + lanzador -----------------------------------------------------
install_app() {
  [[ -d "$SRC_DIR/mchose_hub" && -d "$SRC_DIR/profiles" ]] \
    || die "Ejecuta install.sh desde la carpeta del proyecto (faltan mchose_hub/ o profiles/)."
  log "Instalando la aplicación en $APP_DIR"
  install -d -m 755 "$APP_DIR"
  rm -rf "$APP_DIR/mchose_hub" "$APP_DIR/profiles"
  cp -r "$SRC_DIR/mchose_hub" "$SRC_DIR/profiles" "$APP_DIR/"
  find "$APP_DIR/mchose_hub" -name '__pycache__' -type d -prune -exec rm -rf {} +
  chmod -R a+rX "$APP_DIR"
  install -d -m 755 /etc/mchose-hub/profiles      # perfiles comunes a todos los usuarios

  cat > "$BIN_PATH" <<EOF
#!/bin/sh
# Lanzador generado por install.sh  (desinstalar: bash install.sh --uninstall)
PYTHONPATH="${APP_DIR}\${PYTHONPATH:+:\$PYTHONPATH}" exec "${PYEXEC}" -m mchose_hub "\$@"
EOF
  chmod 755 "$BIN_PATH"
  "$BIN_PATH" --version >/dev/null || die "El lanzador $BIN_PATH no funciona; revisa los mensajes anteriores."
  ok "Lanzador creado: $BIN_PATH"
}

# --- 4. reglas udev -------------------------------------------------------------------
install_udev() {
  log "Generando las reglas udev (perfiles + dispositivos MCHOSE conectados ahora)…"
  if [[ -n "$UDEV_GROUP" ]]; then
    getent group "$UDEV_GROUP" >/dev/null || groupadd --system "$UDEV_GROUP"
    local real_user="${SUDO_USER:-}"
    if [[ -n "$real_user" && "$real_user" != "root" ]]; then
      usermod -aG "$UDEV_GROUP" "$real_user"
      warn "Añadido '$real_user' al grupo '$UDEV_GROUP': cierra sesión y vuelve a entrar para que surta efecto."
    fi
  fi
  if "$BIN_PATH" udev --output "$RULES_FILE" ${UDEV_GROUP:+--group "$UDEV_GROUP"}; then
    ok "Reglas escritas en $RULES_FILE"
  elif [[ -f "$SRC_DIR/packaging/70-mchose-hub.rules" ]]; then
    warn "No se pudieron generar; instalo la copia incluida en packaging/"
    install -d -m 755 "$(dirname "$RULES_FILE")"
    install -m 644 "$SRC_DIR/packaging/70-mchose-hub.rules" "$RULES_FILE"
  else
    die "No se pudieron generar las reglas udev."
  fi
}

# --- 5. acceso directo -----------------------------------------------------------------
install_desktop() {
  log "Creando el acceso directo del menú…"
  install -d -m 755 "$DESKTOP_DIR"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=MCHOSE Linux Hub
GenericName=Configuración de periféricos
Comment=Configura ratones y teclados MCHOSE (DPI, RGB, batería…)
Exec=${BIN_PATH}
Icon=input-mouse
Terminal=false
Categories=Settings;HardwareSettings;GTK;
Keywords=MCHOSE;mouse;keyboard;DPI;RGB;
StartupNotify=true
EOF
  chmod 644 "$DESKTOP_FILE"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  fi
  ok "Acceso directo: $DESKTOP_FILE"
}

# --- programa principal ------------------------------------------------------------------
main() {
  ensure_root
  if (( DO_UNINSTALL )); then uninstall; exit 0; fi

  log "MCHOSE Linux Hub — instalación"
  detect_pm
  if (( INSTALL_DEPS )); then install_deps; else warn "--no-deps: no se instalan paquetes del sistema"; fi
  prepare_python
  install_app
  install_udev
  reload_udev
  install_desktop

  echo
  ok "Instalación completada."
  cat <<EOF

  • Abre «MCHOSE Linux Hub» desde el menú de aplicaciones, o ejecuta:   mchose-hub
  • Ver tus dispositivos con su VID:PID y usage page:                    mchose-hub list --all
  • Probar la interfaz sin hardware:                                      mchose-hub --demo
  • Informe de interfaces y reports HID de tu dispositivo:               mchose-hub inspect
  • Los VID de los MCHOSE conectados durante la instalación ya tienen permisos; si conectas otro (p. ej.
    el dongle, que suele tener otro VID), pulsa «Dar permisos» en la GUI o ejecuta: sudo mchose-hub udev --install
  • Si tu dispositivo ya estaba conectado y no tienes acceso, desconéctalo y vuelve a conectarlo.
  • Añadir un modelo: copia una plantilla de ${APP_DIR}/profiles a ~/.config/mchose-hub/profiles/,
    rellénala (ver README) y ejecuta:   sudo mchose-hub udev --install
EOF
}

main
