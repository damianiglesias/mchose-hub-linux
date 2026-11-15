# MCHOSE Linux Hub

Aplicación Linux para configurar ratones MCHOSE (DPI, polling rate, LOD, RGB, batería, debounce, reposo). Interfaz GTK4/libadwaita.

> Desarrollado y probado con un **MCHOSE A7 V2 Pro** (`5253:1020`, dongle 2.4G 8K). Otros modelos de la familia L7/M7/A7/K7 deberían funcionar — abre un issue con la salida de `mchose-hub list --all` si el tuyo no aparece.

![screenshot](docs/screenshot.png)

## Requisitos

- Linux con GTK 4 + libadwaita 1
- Python ≥ 3.9
- [`mchose-linux`](https://github.com/alexfrih/mchose-linux) instalado (proporciona el backend HID)

## Instalación

```bash
# 1. Instalar el backend HID (una sola vez)
git clone https://github.com/alexfrih/mchose-linux ~/mchose-linux
cd ~/mchose-linux && ./install.sh

# 2. Instalar esta aplicación
git clone https://github.com/TU_USUARIO/mchose-linux-hub
cd mchose-linux-hub
bash install.sh
```

El instalador gestiona las dependencias de Python, las reglas udev (acceso al hardware sin root) y crea el acceso directo en el menú de aplicaciones.

## Uso

Abre **MCHOSE Linux Hub** desde el menú de aplicaciones, o ejecuta:

```bash
mchose-hub
```

Si el dongle no responde al abrir, desconéctalo y vuelve a conectarlo. La aplicación reintenta automáticamente.

## Funcionalidades

- **DPI**: 6 perfiles de 100 a 26 000 DPI, selección del perfil activo
- **Polling rate**: hasta 8000 Hz (inalámbrico y cable por separado)
- **Sensor**: LOD (1/2 mm), ripple control, angle snap, motion sync
- **Debounce**: 0–20 ms
- **Reposo**: tiempo de inactividad configurable
- **Batería**: porcentaje en tiempo real
- **Hot-plug**: detección automática al conectar/desconectar

## Créditos

El protocolo HID fue descifrado por [alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux), que también proporciona el backend CLI. Esta aplicación añade la interfaz gráfica GTK4/libadwaita.

## Licencia

MIT
